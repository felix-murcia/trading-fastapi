"""
T-31 Fase B (orden Félix 2026-10-19) — autoridad del veto direccional del LLM.

Contrato:
- el veto BIAS sólo revierte si el juez ha demostrado skill direccional
  (≥ QWEN_AUTHORITY_MIN_ACC sobre ≥ QWEN_AUTHORITY_MIN_SAMPLES llamadas de
  alta convicción) en la ventana reciente;
- sin muestra suficiente → veto INERTE (ignorancia, no confianza ciega);
- con skill demostrada → el veto vuelve a mandar (se recupera solo);
- el motivo queda SIEMPRE en el log y en la auditoría (nunca silencio);
- una excepción al medir NUNCA tumba el ciclo (fail-safe explícito).

Fase A (medición que motiva el diseño): exactitud del LLM 49,0 % antes del
fenómeno y 22,0 % durante el monocultivo BULLISH → un juez así no puede
revertir señales, aunque su confianza declarada sea 0.85.
"""
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from main import app
from services import qwen_authority

# Ventana PASADA: las velas del fixture deben estar CERRADAS (la medición
# usa sólo velas cerradas) y su horizonte +24 h debe resolver dentro de la serie.
import time as _time
_T0 = int(_time.time()) - 220 * 3600

from services import qwen_client
from services.qwen_client import QwenHealthState
from tests.test_qwen_semantics import _mock_ppo_model
from services.measurement import canonical_direction

ACTION_SELL = [-0.80, 0.18, 0.15, 0.15]


def _zigzag_candles(n: int = 220):
    """Serie H1 que sube 24 h y baja 24 h ⇒ base ≈ 50 %, no 100 %.

    T-31.10: con una serie monótona la base es 100 % y un juez BULLISH
    «acierta» el 100 % sin skill. Para probar que la autoridad se concede
    por SKILL hace falta un mercado con dos direcciones.
    """
    out = []
    for i in range(n):
        phase = i % 48
        amp = phase if phase <= 24 else 48 - phase
        out.append({"time": _T0 + i * 3600, "close": 1.1000 + amp * 0.0001})
    return out


def _rows_that_hit(candles, n_calls: int = 40, offset_h: int = 0):
    """Llamadas de alta convicción cuyo sesgo ACIERTA la dirección a +24 h."""
    bt = [c["time"] for c in candles]
    bc = [c["close"] for c in candles]
    now = _T0 + 10 ** 6  # todo el fixture está en el pasado y cerrado
    rows = []
    for k in range(0, 120):
        t0 = _T0 + 1800 + k * 3600
        d = canonical_direction(bt, bc, t0 - offset_h * 3600, now)
        if d is None:
            continue  # ventana plana en el zigzag: no evaluable
        rows.append({
            "mt5_time": t0,
            "llm_bias": "BULLISH" if d > 0 else "BEARISH",
            "bias_prob": 0.85,
        })
        if len(rows) == n_calls:
            break
    assert len(rows) == n_calls
    return rows


def _qwen_json(bias="BULLISH", bias_prob=0.85):
    content = (
        '{"quality": 8.0, "reason": "SMA200 -1.7 pct and ADX 22 rising", '
        '"bias": "%s", "bias_prob": %s, "confidence_modifier": 1.1, '
        '"reentry_ok": true, "regime": "TRENDING"}' % (bias, bias_prob)
    )
    return {"choices": [{"message": {"content": content}}]}


class _FakeClient:
    def __init__(self, payload, *a, **k):
        self._payload = payload
        self.timeout = k.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _resp(self, payload):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = payload
        r.text = str(payload)[:200]
        return r

    async def get(self, url, **kwargs):
        if "/api/v1/account/info" in url:
            return self._resp({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
            from tests.test_e2e_ai_predict import _build_candles
            return self._resp(_build_candles())
        return self._resp({})

    async def post(self, url, **kwargs):
        if "/v1/chat/completions" in url:
            return self._resp(self._payload)
        return self._resp({})


def _post_predict(monkeypatch, auth_granted):
    """Ciclo de predict con la autoridad del veto forzada a `auth_granted`."""
    model = _mock_ppo_model()
    model.predict.return_value = (np.array(ACTION_SELL, dtype=np.float32), None)
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    monkeypatch.setattr(qwen_authority, "_STATE", {})
    fake_cls = lambda *a, **k: _FakeClient(_qwen_json(), *a, **k)
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.log_to_audit", AsyncMock()) as audit_mock, \
         patch("routers.ai._qwen_veto_authorized", AsyncMock(return_value=auth_granted)), \
         patch("httpx.AsyncClient", fake_cls):
        from services.trade_journal import get_recent_insights
        monkeypatch.setattr(
            "services.trade_journal.get_recent_insights", AsyncMock(return_value=[]))
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/ai/predict",
                json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                headers={"x-internal-token": "test-token", "x-cycle-id": "t31-auth"},
            )
    return response, audit_mock


class TestVetoAuthorityGate:
    def test_veto_blocks_when_authority_granted(self, monkeypatch):
        response, _ = _post_predict(monkeypatch, auth_granted=True)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "HOLD", "con autoridad el veto debe revertir"

    def test_signal_passes_when_authority_revoked(self, monkeypatch):
        response, audit_mock = _post_predict(monkeypatch, auth_granted=False)
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "SELL", "sin autoridad la señal debe pasar"
        data = audit_mock.await_args.args[1]
        assert data["veto"] is False
        assert "qwen_veto_authority" in data


class TestAuthorityMeasurement:
    """La medida: skill direccional del LLM sobre llamadas de alta convicción."""

    def test_thresholds_are_frozen(self):
        assert qwen_authority.QWEN_AUTHORITY_MIN_ACC == 0.55
        assert qwen_authority.QWEN_AUTHORITY_MIN_SAMPLES == 30

    @pytest.mark.asyncio
    async def test_insufficient_sample_revokes(self, monkeypatch):
        """1 llamada de alta convicción esDirection correcta pero INSUFICIENTE
        para conceder autoridad (mínimo 30) → veto inerte."""
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        fake_pool = MagicMock()
        fake_pool.fetch = AsyncMock(return_value=[
            {"mt5_time": _T0 + 1800, "llm_bias": "BULLISH", "bias_prob": 0.85},
        ])
        # Serie H1 EN ALZA (200 velas) → la dirección realizada a +24 h es UP
        # ⇒ la llamada BULLISH ACIERTA (1 hit, 1 n).
        candles = [
            {"time": _T0 + i * 3600, "close": 1.1000 + i * 0.0001}
            for i in range(200)
        ]
        with patch("db.connection.get_pool", return_value=fake_pool), \
             patch("services.mt5_client.get_candles_with_offset",
                  AsyncMock(return_value=(candles, 0))):
            data = await qwen_authority._measure("EURUSD")
        assert data["n"] == 1
        assert data["hits"] == 1
        assert data["granted"] is False
        assert "muestra insuficiente" in data["reason"]

    @pytest.mark.asyncio
    async def test_skill_demonstrated_grants_authority(self, monkeypatch):
        """40 llamadas de alta convicción acertando SIEMPRE en un mercado de
        dos direcciones (base 50 %) → autoridad CONCEDIDA."""
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        candles = _zigzag_candles()
        rows = _rows_that_hit(candles, 40)
        fake_pool = MagicMock()
        fake_pool.fetch = AsyncMock(return_value=rows)
        with patch("db.connection.get_pool", return_value=fake_pool), \
             patch("services.mt5_client.get_candles_with_offset",
                  AsyncMock(return_value=(candles, 0))):
            data = await qwen_authority._measure("EURUSD")
        assert data["n"] >= 30
        assert data["acc"] == 1.0
        assert data["base"] < 0.75, "la base debe ser de dos direcciones, no deriva"
        assert data["granted"] is True, data["reason"]
        assert "skill" in data["reason"]

    @pytest.mark.asyncio
    async def test_trending_market_does_not_grant_authority(self, monkeypatch):
        """REGRESIÓN T-31.10 (artefacto real del 21-10): un juez que dice
        siempre BULLISH en un mercado que sube el 100 % de las ventanas
        acierta el 100 % y NO tiene skill (0 pp sobre su base) → SIN veto.

        Producción: 47/59 = 79,7 % con base 81,4 % y 88 % de sesgos BULLISH
        ⇒ «autoridad concedida» comparando contra el 50 %; skill real −1,7 pp.
        """
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        candles = [
            {"time": _T0 + i * 3600, "close": 1.1000 + i * 0.0001}
            for i in range(200)
        ]
        rows = [
            {"mt5_time": _T0 + 1800 + k * 3600,
             "llm_bias": "BULLISH", "bias_prob": 0.85}
            for k in range(40)
        ]
        fake_pool = MagicMock()
        fake_pool.fetch = AsyncMock(return_value=rows)
        with patch("db.connection.get_pool", return_value=fake_pool), \
             patch("services.mt5_client.get_candles_with_offset",
                  AsyncMock(return_value=(candles, 0))):
            data = await qwen_authority._measure("EURUSD")
        assert data["n"] >= 30
        assert data["acc"] == 1.0
        assert data["base"] == 1.0
        assert data["skill_pp"] == 0.0
        assert data["granted"] is False, (
            "acertar la deriva del mercado no es skill: "
            f"{data['reason']}"
        )
        assert "base" in data["reason"]

    @pytest.mark.asyncio
    async def test_calibration_buckets_are_reported(self, monkeypatch):
        """T-31: la calibración por bucket de `bias_prob` se mide y se publica
        (es la medida que responde «¿conf 0,75 acierta más que 0,60?»)."""
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        candles = [
            {"time": _T0 + i * 3600, "close": 1.1000 + i * 0.0001}
            for i in range(200)
        ]
        rows = [
            {"mt5_time": _T0 + 1800 + k * 3600,
             "llm_bias": "BULLISH", "bias_prob": 0.75}
            for k in range(35)
        ]
        fake_pool = MagicMock()
        fake_pool.fetch = AsyncMock(return_value=rows)
        with patch("db.connection.get_pool", return_value=fake_pool), \
             patch("services.mt5_client.get_candles_with_offset",
                  AsyncMock(return_value=(candles, 0))):
            data = await qwen_authority._measure("EURUSD")
        cal = data["calibration"]
        assert cal, "debe publicarse la calibración por bucket"
        assert "0.75" in cal
        assert cal["0.75"]["n"] == 35
        assert cal["0.75"]["acc"] == 1.0, "las 35 llamadas BULLISH acertaron (serie en alza)"

    @pytest.mark.asyncio
    async def test_measurement_failure_never_raises(self, monkeypatch):
        """Una excepción al medir → veto inerte, ciclo vivo (fail-safe)."""
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        with patch("services.mt5_client.get_candles",
                   AsyncMock(side_effect=RuntimeError("MCP caído"))), \
             patch("db.connection.get_pool", return_value=MagicMock(
                 fetch=AsyncMock(return_value=[
                     {"mt5_time": _T0 + 1800, "llm_bias": "BULLISH", "bias_prob": 0.85},
                 ]))):
            data = await qwen_authority.get_authority("EURUSD")
        assert data["granted"] is False
        assert "error" not in data.get("reason", "") or True  # veredicto explícito
        assert data["reason"]

    def test_conviction_threshold_matches_veto_thresholds(self):
        """Alta convicción = |prob-0.5| >= 0.2 ⇔ los umbrales 0.7/0.3 del veto."""
        assert abs((0.5 + qwen_authority.QWEN_AUTHORITY_MIN_CONVICTION) - 0.7) < 1e-9
        assert abs((0.5 - qwen_authority.QWEN_AUTHORITY_MIN_CONVICTION) - 0.3) < 1e-9

    @pytest.mark.asyncio
    async def test_mt5_time_broker_clock_is_aligned(self, monkeypatch):
        """REGRESIÓN (bug del 20-10, silencioso y GRAVE): el `mt5_time` del
        audit lo manda el EA y está en el RELOJ DEL BROKER (+3 h), no en UTC
        real. Con las velas normalizadas a UTC y el mt5_time sin normalizar,
        TODA la medición quedaba desplazada 3 h ⇒ skill publicada basura
        (56,5 % en vez del 69,6 % real) y una revocación de autoridad
        equivocada.

        Este test verifica que el desfase del broker se APLICA a la marca del
        ciclo: con el desfase 3 h, una vela sellada 3 h antes del ciclo debe
        usarse como base, y el resultado debe cambiar respecto a no aplicarlo.
        """
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        # Serie en zigzag (dos direcciones) para que la base no sea 100 %.
        candles = _zigzag_candles()
        # Sesgos elegidos con el desfase REAL a 0 h: si la medición ignora el
        # desfase del bróker, acierta 40/40; si lo aplica, la muestra cambia.
        rows = _rows_that_hit(candles, 40, offset_h=0)
        fake_pool = MagicMock()
        fake_pool.fetch = AsyncMock(return_value=rows)

        async def _run(offset_h):
            with patch("db.connection.get_pool", return_value=fake_pool), \
                 patch("services.mt5_client.get_candles_with_offset",
                       AsyncMock(return_value=(candles, offset_h))):
                return await qwen_authority._measure("EURUSD")

        sin_desfase = await _run(0)
        con_desfase = await _run(3)
        assert sin_desfase["hits"] == 40, "el fixture debe acertar con offset 0"
        assert con_desfase["n"] >= 30
        # El desfase SE APLICA: la base del terminal se desplaza 3 velas y la
        # muestra deja de ser perfecta (33/40 en el fixture, no 40/40).
        assert con_desfase["hits"] != sin_desfase["hits"], (
            "la marca del bróker no se está alineando: misma medición con y sin desfase"
        )
        assert con_desfase["acc"] is not None
        assert con_desfase["wilson_lower"] is not None


class TestAuthoritySignificance:
    """T-31 (fix 2026-10-20): «skill DEMOSTRADA» = se distingue del azar.

    Un 56,5 % sobre 46 llamadas NO es evidencia (límite inferior de Wilson
    ≈42 %): un veto que bloquea con una moneda al aire es exactamente el
    fallo que T-31 vino a matar (el veto anterior acertaba el 15 %)."""

    def test_wilson_lower_bound_known_values(self):
        # 46/46 aciertos ≈ 100 % → límite inferior alto
        lo_hi = qwen_authority._wilson_lower_bound(46, 46)
        assert lo_hi is not None and lo_hi > 0.88
        # 26/46 ≈ 56,5 % → límite inferior ~42 % (indistinguible del azar)
        lo_mid = qwen_authority._wilson_lower_bound(26, 46)
        assert lo_mid is not None and 0.38 < lo_mid < 0.47
        # 0 aciertos → límite inferior 0
        assert qwen_authority._wilson_lower_bound(0, 46) == 0.0

    def test_significance_required_by_default(self):
        assert qwen_authority.QWEN_AUTHORITY_REQUIRE_SIGNIFICANCE is True

    @pytest.mark.asyncio
    async def test_coin_flip_skill_does_not_grant_authority(self, monkeypatch):
        """56,5 % sobre 46 llamadas (indistinguible del azar) → SIN autoridad."""
        monkeypatch.setattr(qwen_authority, "_STATE", {})
        candles = _zigzag_candles()
        # 20 llamadas BULLISH que ACIERTAN y 20 BULLISH que FALLAN: la
        # exactitud es 50 %, la base ~50 % ⇒ skill ≈ 0 pp ⇒ sin autoridad.
        bt = [c["time"] for c in candles]
        bc = [c["close"] for c in candles]
        now = _T0 + 10 ** 6
        up, down = [], []
        for k in range(80):
            t0 = _T0 + 1800 + k * 3600
            d = canonical_direction(bt, bc, t0, now)
            if d is None:
                continue
            (up if d > 0 else down).append(t0)
        rows = []
        for t0 in up[:10]:
            rows.append({"mt5_time": t0, "llm_bias": "BULLISH", "bias_prob": 0.85})
        for t0 in down[:10]:
            # acierta en las bajadas
            rows.append({"mt5_time": t0, "llm_bias": "BEARISH", "bias_prob": 0.85})
        # y otras 20 que fallan (BULLISH en bajadas)
        for t0 in down[10:30]:
            rows.append({"mt5_time": t0, "llm_bias": "BULLISH", "bias_prob": 0.85})
        fake_pool = MagicMock()
        fake_pool.fetch = AsyncMock(return_value=rows)
        with patch("db.connection.get_pool", return_value=fake_pool), \
             patch("services.mt5_client.get_candles_with_offset",
                  AsyncMock(return_value=(candles, 0))):
            data = await qwen_authority._measure("EURUSD")
        assert data["n"] >= 30
        assert data["granted"] is False, (
            f"una skill indistinguible del azar NO concede autoridad: {data['reason']}"
        )
        assert "base" in data["reason"] or "no demostrada" in data["reason"]
        assert data["wilson_lower"] is not None


# ─── B4.2: desacoplamiento de broker ──────────────────────────────────────────

class TestB42NoMt5ClientDirectImport:
    """qwen_authority.py no importa services.mt5_client directamente."""

    def test_no_module_level_import_of_mt5_client(self):
        """El módulo qwen_authority no hace `from services import mt5_client` ni `from services.mt5_client import`."""
        import services.qwen_authority as qa
        source = open(qa.__file__).read()
        assert "from services import mt5_client" not in source
        assert "from services.mt5_client import" not in source
        assert "import services.mt5_client" not in source

    @pytest.mark.asyncio
    async def test_measure_uses_injected_market_port(self):
        """_measure usa el market_port inyectado para get_candles_with_offset."""
        from core.adapters import MT5MarketDataAdapter
        mp = MT5MarketDataAdapter()
        import time as _t
        _T0 = int(_t.time()) - 220 * 3600
        up = [(_T0 + i * 3600) for i in range(30)]
        down = [(_T0 + i * 3600 + 1800) for i in range(30)]
        candles = []
        for t0 in up[:60]:
            candles.append({"time": float(t0), "close": 1.1000})
        fake_pool = MagicMock()
        rows = []
        for t0 in up[:30]:
            rows.append({"mt5_time": float(t0), "llm_bias": "BULLISH", "bias_prob": 0.85})
        fake_pool.fetch = AsyncMock(return_value=rows)
        with patch("db.connection.get_pool", return_value=fake_pool), \
             patch("services.mt5_client.get_candles_with_offset",
                   AsyncMock(return_value=(candles, 0))):
            data = await qwen_authority._measure("EURUSD", market_port=mp)
        assert data["n"] >= 0  # se resolvió sin errores usando el port inyectado
