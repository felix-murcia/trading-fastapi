"""
T-31 (orden Félix 2026-10-19) — QUÓRUM ENTRE MODELOS EN MERCADO LATERAL.

Qué se prueba
-------------
1. UNIDAD — la lógica del quórum (services/quorum.py): detección lateral
   determinista, voto de cada fuente, veredicto.
2. INTEGRACIÓN — con un mercado LATERAL REAL (velas oscilantes que pasan
   por el pipeline real de features: `engineer_market_features`), el ciclo
   de /predict:
   - PPO y LLM en desacuerdo → SIN quórum → HOLD (no se entra en rango);
   - PPO y LLM de acuerdo → quórum → la señal pasa;
   - LLM abstenido → su voto NO cuenta (no hay evidencia) → el quórum
     exige el acuerdo de las otras dos fuentes;
   - régimen NO lateral → el quórum NO se aplica (cero cambios de
     comportamiento fuera del lateral: el PPO decide).
"""
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from main import app
from services import qwen_client, quorum
from services.qwen_client import QwenHealthState
from tests.test_qwen_semantics import _mock_ppo_model

ACTION_SELL = [-0.80, 0.18, 0.15, 0.15]
ACTION_BUY = [0.80, 0.18, 0.15, 0.15]


# ───────────────────────── helpers de mercado ─────────────────────────

def _lateral_candles(n: int = 300) -> list[dict]:
    """Velas LATERALES H1: precio oscilando en banda estrecha, sin tendencia.

    Se construyen con el MISMO formato que devuelve el MCP real (time ISO
    UTC) y se pasan por el pipeline real de features
    (`engineer_market_features`), de modo que el detector de lateral juzga
    sobre NÚMEROS REALES y no sobre un mock.

    Reproduce la patología medida en vivo el 2026-10-19: oscilación
    diaria (periodo 24 h, amplitud 50 pips) ⇒ el precio NO se mueve
    (desplazamiento 24 h = 0,29 ATR) pero el ADX de H1 sale **0,48**
    ("tendencia"), porque el ADX mide el ratio de movimiento direccional
    sobre volatilidad, no cuánto se ha movido el precio. Con el criterio
    antiguo (ADX) este mercado pasaba como tendencial y el quórum no
    entraba; con el de desplazamiento (≤1,5 ATR) se declara LATERAL.
    """
    import datetime as dt
    rng = np.random.default_rng(5)
    base_ts = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    candles = []
    for i in range(n):
        px = 1.1000 + 0.0005 * np.sin(2 * np.pi * i / 24.0) + float(rng.normal(0, 0.00005))
        prev = (1.1000 + 0.0005 * np.sin(2 * np.pi * (i - 1) / 24.0)
                + float(rng.normal(0, 0.00005)))
        candles.append({
            "time": (base_ts + dt.timedelta(hours=n - 1 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(float(prev), 5),
            "high": round(float(max(px, prev)) + 0.00004, 5),
            "low": round(float(min(px, prev)) - 0.00004, 5),
            "close": round(float(px), 5),
            "tick_volume": 1000 + (i % 50),
        })
    return candles  # orden ascendente; el harness de predict lo ordena


def _trending_candles(n: int = 260) -> list[dict]:
    """Velas con TENDENCIA clara (para el caso 'régimen no lateral')."""
    import datetime as dt
    base_ts = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    candles = []
    for i in range(n):
        px = 1.1000 + i * 0.00025
        candles.append({
            "time": (base_ts + dt.timedelta(minutes=5 * (n - 1 - i))).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(px - 0.0001, 5),
            "high": round(px + 0.0004, 5),
            "low": round(px - 0.0004, 5),
            "close": round(px, 5),
            "tick_volume": 1000 + (i % 50),
        })
    return candles


def _disp24_atr(df) -> float:
    """Desplazamiento 24 h por ATR14 sobre el df engineers (mismo cálculo
    que routers/ai._displacement_24h_atr)."""
    import pandas as pd
    times = pd.to_datetime(df["time"], errors="coerce", utc=True)
    close = df["close"].astype(float)
    atr = float(df["atr"].iloc[-1])
    target = times.iloc[-1] - pd.Timedelta(hours=24)
    mask = times <= target
    if not mask.any() or not atr > 0:
        return 99.0
    return abs(float(close.iloc[-1]) - float(close[mask].iloc[-1])) / atr


def _qwen_json(bias="BULLISH", bias_prob=0.85, reason="SMA200 -1.7% and ADX 22 rising"):
    content = (
        '{"quality": 8.0, "reason": "%s", "bias": "%s", "bias_prob": %s, '
        '"confidence_modifier": 1.1, "reentry_ok": true, "regime": "TRENDING"}'
        % (reason, bias, bias_prob)
    )
    return {"choices": [{"message": {"content": content}}]}


class _FakeClient:
    """Mock del MCP: sirve la serie pedida y AGREGA a H4/D1 como el broker
    (4 velas H1 → 1 H4, 24 → 1 D1). Sin esa agregación, el ADX del marco
    superior sería el de H1 y el test no mediría lo que dice medir."""

    def __init__(self, payload, candles, *a, **k):
        self._payload = payload
        self._candles = candles
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

    @staticmethod
    def _aggregate(candles, k):
        out = []
        for i in range(0, len(candles) - k + 1, k):
            g = candles[i:i + k]
            out.append({
                "time": g[-1]["time"],
                "open": g[0]["open"],
                "high": max(x["high"] for x in g),
                "low": min(x["low"] for x in g),
                "close": g[-1]["close"],
                "tick_volume": 1000,
            })
        return out

    async def get(self, url, **kwargs):
        if "/api/v1/account/info" in url:
            return self._resp({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
            tf = "H1"
            if "timeframe=H4" in url:
                tf = "H4"
            elif "timeframe=D1" in url:
                tf = "D1"
            k = {"H1": 1, "H4": 4, "D1": 24}[tf]
            return self._resp(self._aggregate(self._candles, k))
        return self._resp({})

    async def post(self, url, **kwargs):
        if "/v1/chat/completions" in url:
            return self._resp(self._payload)
        return self._resp({})


def _post(monkeypatch, action, qwen_json, candles, auth_granted=True):
    model = _mock_ppo_model()
    model.predict.return_value = (np.array(action, dtype=np.float32), None)
    monkeypatch.setattr(qwen_client, "_QWEN_STATE", QwenHealthState())
    fake_cls = lambda *a, **k: _FakeClient(qwen_json, candles, *a, **k)
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
                headers={"x-internal-token": "test-token", "x-cycle-id": "t31-quorum"},
            )
    return response, audit_mock


# ───────────────────────────── UNIDAD ─────────────────────────────

class TestLateralDetection:
    def test_fixture_market_is_really_lateral(self):
        """AUTO-VERIFICACIÓN DEL FIXTURE: las velas del test deben ser
        laterales de verdad según el MISMO detector que usa producción
        (desplazamiento 24 h + ADX H4 + bb_pos/atr_pct/macd_hist sobre el
        pipeline real de features). Si el fixture dejara de ser lateral, los
        tests de integración probarían otra cosa (o nada).

        Además verifica que el fixture reproduce LA PATOLOGÍA: ADX de H1
        alto ("tendencia") con el precio inmóvil — el caso exacto que dejó
        pasar el SELL en rango del 19-10."""
        import pandas as pd
        from ml.trading_env_v2 import engineer_market_features
        df = engineer_market_features(pd.DataFrame(_lateral_candles()))
        last = df.iloc[-1]
        atr = float(last["atr"])
        close = float(last["close"])
        disp = _disp24_atr(df)
        adx1 = float(last["adx"])
        assert adx1 > 0.30, (
            f"el fixture debe reproducir el ADX engañoso (medido {adx1:.3f})"
        )
        assert disp < quorum.DISP_MAX_ATR, (
            f"el fixture debe tener el precio inmóvil (disp24/ATR={disp:.2f})"
        )
        assert quorum.detect_lateral(
            bb_pos=float(last["bb_pos"]),
            atr_pct=atr / close,
            macd_hist=float(last["macd_hist"]),
            adx=adx1,
            disp24_atr=disp,
            adx_h4=0.20,
        ) is True, (
            f"el fixture de mercado lateral dejó de ser lateral: "
            f"bb_pos={float(last['bb_pos']):.3f} atr_pct={atr/close:.5f} "
            f"macd={float(last['macd_hist']):.6f} adx={adx1:.3f} disp24_atr={disp:.2f}"
        )

    def test_displacement_criterion(self):
        """T-31 (2026-10-19) — el criterio que sustituye al ADX de H1.

        El ADX mide el RATIO de movimiento direccional sobre volatilidad, no
        cuánto se ha movido el precio: con ADX H1 = 33 (¡"tendencia"!) y un
        desplazamiento de 24 h de 1,16 ATR, el mercado estaba en un RANGO
        (54 pips) y el detector lo dejó pasar → SELL con el SL dentro del
        rango y el TP fuera. Con el criterio de desplazamiento (≤1,5 ATR) el
        mismo mercado se declara LATERAL."""
        # Rango: el precio no se ha movido (1,2 ATR en 24 h) → lateral
        assert quorum.detect_lateral(
            bb_pos=-0.315, atr_pct=0.00122, macd_hist=-0.000043,
            adx=0.331, disp24_atr=1.16, adx_h4=0.219,
        ) is True, "un mercado de 1,16 ATR de desplazamiento es lateral (ADX H1 engañoso)"
        # Tendencia real: el precio se ha movido (3 ATR) y la estructura tiene
        # dirección → NO lateral
        assert quorum.detect_lateral(
            bb_pos=-0.315, atr_pct=0.00122, macd_hist=-0.000043,
            adx=0.331, disp24_atr=3.0, adx_h4=0.219, slope_h1_ratio=0.0005,
        ) is False
        # Rango por desplazamiento pero con TENDENCIA en H4 → no lateral
        assert quorum.detect_lateral(
            bb_pos=-0.315, atr_pct=0.00122, macd_hist=-0.000043,
            adx=0.331, disp24_atr=1.16, adx_h4=0.30,
        ) is False, "el marco superior con tendencia manda"

    def test_flat_structure_is_a_chop_signal(self):
        """Segunda señal de chop: la estructura H1 PLANA (pendiente < 0,02 %)
        aunque el precio se haya movido algo en 24 h. Caso real del 19-10:
        desplazamiento 2,1 ATR pero SMA200 H1 con pendiente +0,003 % y H4
        sin tendencia (ADX 0,219) ⇒ chop ⇒ el quórum aplica."""
        assert quorum.detect_lateral(
            bb_pos=-0.381, atr_pct=0.00119, macd_hist=-0.000054,
            adx=0.355, disp24_atr=2.137, adx_h4=0.219, slope_h1_ratio=0.00003,
        ) is True, "H4 sin tendencia + estructura H1 plana = chop"
        # Con estructura H1 con dirección (pendiente > 0,02 %) → no lateral
        assert quorum.detect_lateral(
            bb_pos=-0.381, atr_pct=0.00119, macd_hist=-0.000054,
            adx=0.355, disp24_atr=2.137, adx_h4=0.219, slope_h1_ratio=0.0005,
        ) is False

    def test_adx_h1_alone_no_longer_decides(self):
        """T-31: el ADX de H1 deja de ser el juez del régimen (medía el
        ratio de movimiento direccional, no el desplazamiento: decía
        "tendencia" con el precio en un rango). Sin señal de chop
        (desplazamiento <=1,5 ATR o estructura plana) NO se declara lateral."""
        assert quorum.detect_lateral(
            bb_pos=0.1, atr_pct=0.004, macd_hist=0.00005, adx=0.14,
        ) is False, "sin señal de chop no se declara lateral"
        # con señal de chop (precio inmóvil) → lateral, aunque el ADX sea alto
        assert quorum.detect_lateral(
            bb_pos=0.1, atr_pct=0.004, macd_hist=0.00005, adx=0.48,
            disp24_atr=0.3, adx_h4=0.20,
        ) is True

    def test_adx_scale_is_normalized(self):
        """REGRESIÓN (bug real, detectado en vivo 2026-10-19): la columna
        `adx` del df viene NORMALIZADA a [0,1] (`DX medio / 100` en
        engineer_market_features). Con el umbral 20.0 (escala clásica) TODO
        el mercado se declaraba lateral y el quorum bloqueaba el 100 % de
        las entradas. El umbral debe ir en la escala del df (0,25), igual
        que el reward del env (`is_ranging = adx < 0.25`).

        Este test blinda la ESCALA del umbral de marco superior."""
        assert quorum.ADX_TREND_MIN == 0.25, (
            "el umbral de ADX debe estar en la escala normalizada [0,1] del df"
        )
        assert quorum.ADX_H4_TREND_MIN == 0.25
        # Tendencia (ADX normalizado 0.33 ~ 33 clasico) -> NO lateral
        assert quorum.detect_lateral(
            bb_pos=0.1, atr_pct=0.004, macd_hist=0.00005,
            disp24_atr=0.3, adx_h4=0.33,
        ) is False
        # Rango (ADX normalizado 0.10 ~ 10 clasico) -> lateral
        assert quorum.detect_lateral(
            bb_pos=0.1, atr_pct=0.004, macd_hist=0.00005,
            disp24_atr=0.3, adx_h4=0.10,
        ) is True

    def test_flat_market_is_lateral(self):
        assert quorum.detect_lateral(
            bb_pos=0.1, atr_pct=0.004, macd_hist=0.00005,
            disp24_atr=0.4, adx_h4=0.14,
        ) is True

    def test_trend_adx_blocks_lateral(self):
        """Con ADX de tendencia en H4, NO es lateral aunque el resto diga rango."""
        assert quorum.detect_lateral(
            bb_pos=0.1, atr_pct=0.004, macd_hist=0.00005,
            disp24_atr=0.4, adx_h4=0.28,
        ) is False

    def test_bb_pos_extreme_is_not_lateral(self):
        assert quorum.detect_lateral(
            bb_pos=0.95, atr_pct=0.004, macd_hist=0.00005,
            disp24_atr=0.4, adx_h4=0.12,
        ) is False

    def test_high_volatility_is_not_lateral(self):
        assert quorum.detect_lateral(
            bb_pos=0.1, atr_pct=0.02, macd_hist=0.00005,
            disp24_atr=0.4, adx_h4=0.12,
        ) is False

    def test_missing_data_is_not_lateral(self):
        """Sin datos NO se declara lateral (fail-safe: no se bloquea por
        falta de evidencia)."""
        assert quorum.detect_lateral(None, 0.004, 0.00005, adx=0.12) is False


class TestVoteCollection:
    def test_structure_vote_requires_confirmation(self):
        assert quorum.structure_vote(0.012, 0.003) == "BUY"
        assert quorum.structure_vote(-0.012, -0.003) == "SELL"
        # distancia y pendiente contradictorias → no vota
        assert quorum.structure_vote(0.012, -0.003) is None
        assert quorum.structure_vote(None, 0.003) is None

    def test_structure_vote_ignores_flat_slope(self):
        """T-31: una pendiente de +0,003 % (ruido en un rango) NO cuenta como
        confirmación de tendencia. La pendiente se normaliza por el precio
        (ratio), el mismo criterio y constante que el reward del env
        (REWARD_V2_S0_SLOPE = 0,0002 = 0,02 %)."""
        # ratio 0,00003 (ruido) → no vota
        assert quorum.structure_vote(0.0012, 0.00003, price=1.12) is None
        # ratio 0,0003 (dirección clara) → vota BUY
        assert quorum.structure_vote(0.0012, 0.0003, price=1.12) == "BUY"
        # SELL con pendiente negativa clara
        assert quorum.structure_vote(-0.0012, -0.0003, price=1.12) == "SELL"

    def test_llm_vote_requires_evidence_and_conviction(self):
        assert quorum.llm_vote("BULLISH", 0.85, abstained=False) == "BUY"
        assert quorum.llm_vote("BEARISH", 0.25, abstained=False) == "SELL"
        # abstención (sin evidencia) → no vota
        assert quorum.llm_vote("BULLISH", 0.85, abstained=True) is None
        # indeciso → no vota
        assert quorum.llm_vote("BULLISH", 0.52, abstained=False) is None
        assert quorum.llm_vote("NEUTRAL", 0.85, abstained=False) is None

    def test_quorum_requires_two_aligned_sources(self):
        c = quorum.collect_votes("SELL", llm_bias="BEARISH", llm_bias_prob=0.25,
                                 sma200_dist=-0.012, sma200_slope=-0.003)
        v = quorum.evaluate(c)
        assert v["granted"] is True and v["direction"] == "SELL"

    def test_no_quorum_when_sources_disagree(self, monkeypatch=None):
        """PPO SELL, LLM BULLISH y estructura BUY: el quórum CONFIRMA la
        dirección del PPO — 2 votos BUY NO autorizan una entrada BUY
        (el PPO es la señal, el quórum es el filtro)."""
        c = quorum.collect_votes("SELL", llm_bias="BULLISH", llm_bias_prob=0.85,
                                 sma200_dist=0.012, sma200_slope=0.003)
        v = quorum.evaluate(c, direction="SELL")
        assert v["granted"] is False
        assert v["tally"] == {"BUY": 2, "SELL": 1}
        assert v["direction"] is None

    def test_single_vote_never_reaches_quorum(self):
        """PPO SELL + LLM BULLISH: dos fuentes votan en direcciones
        opuestas → ningún lado alcanza 2."""
        c = quorum.collect_votes("SELL", llm_bias="BULLISH", llm_bias_prob=0.85)
        v = quorum.evaluate(c, direction="SELL")
        assert v["granted"] is False
        assert v["tally"] == {"BUY": 1, "SELL": 1}
        assert v["n_votes"] == 2

    def test_quorum_confirms_ppo_direction_only(self):
        """Regla de oro: el quórum confirma la dirección del PPO."""
        c = quorum.collect_votes("SELL", llm_bias="BULLISH", llm_bias_prob=0.85,
                                 sma200_dist=0.012, sma200_slope=0.003)
        v = quorum.evaluate(c, direction="SELL")
        assert v["granted"] is False, "no puede autorizar la contraria"


# ─────────────────────────── INTEGRACIÓN ───────────────────────────

class TestQuorumInLateralMarket:
    """Mercado LATERAL real (velas oscilantes → features reales)."""

    def test_disagreement_blocks_entry(self, monkeypatch):
        """PPO SELL + LLM BULLISH en lateral → SIN quórum → HOLD."""
        resp, audit_mock = _post(
            monkeypatch, ACTION_SELL, _qwen_json("BULLISH", 0.85), _lateral_candles())
        assert resp.status_code == 200, resp.text
        data = audit_mock.await_args.args[1]
        q = data["quorum"]
        assert q is not None, "el ciclo debe declarar el veredicto de quórum"
        assert q["lateral"] is True, "el mercado de test debe detectarse lateral"
        assert q["granted"] is False
        assert data["decision"] == "HOLD", "sin consenso en lateral no se entra"
        assert resp.json()["decision"] == "HOLD"

    def test_agreement_allows_entry(self, monkeypatch):
        """PPO SELL + LLM BEARISH en lateral → quórum → la señal pasa."""
        resp, audit_mock = _post(
            monkeypatch, ACTION_SELL, _qwen_json("BEARISH", 0.25), _lateral_candles())
        assert resp.status_code == 200, resp.text
        data = audit_mock.await_args.args[1]
        q = data["quorum"]
        assert q["lateral"] is True
        assert q["granted"] is True
        assert q["direction"] == "SELL"
        assert data["decision"] == "SELL"

    def test_abstained_llm_does_not_vote(self, monkeypatch):
        """LLM sin evidencia (abstención) → su voto NO cuenta: sin quórum
        si el resto no coincide (el juez no puede \"bloquear\" ni \"avalar\"
        con una respuesta de plantilla)."""
        resp, audit_mock = _post(
            monkeypatch, ACTION_SELL, _qwen_json("BULLISH", 0.85,
                                                 reason="trend looks fine"),
            _lateral_candles())
        assert resp.status_code == 200, resp.text
        data = audit_mock.await_args.args[1]
        assert data["qwen_abstained"] is True
        q = data["quorum"]
        assert q["votes"]["llm"] is None, "la abstención no es un voto"
        assert q["granted"] is False
        assert data["decision"] == "HOLD"


class TestQuorumNotAppliedOutsideLateral:
    def test_trending_market_keeps_ppo_decision(self, monkeypatch):
        """En régimen NO lateral el quorum NO se aplica: el PPO decide
        (cero cambios de comportamiento fuera del lateral).

        El LLM va ALINEADO con el PPO (BEARISH) para que este test mida
        sólo el quorum, no el veto de sesgo (que se prueba en su propio
        fichero)."""
        resp, audit_mock = _post(
            monkeypatch, ACTION_SELL, _qwen_json("BEARISH", 0.25), _trending_candles())
        assert resp.status_code == 200, resp.text
        data = audit_mock.await_args.args[1]
        q = data["quorum"]
        assert q is not None and q["lateral"] is False, (
            "el mercado con tendencia NO debe declararse lateral"
        )
        assert q["granted"] is None, "sin quorum no hay veredicto"
        assert data["decision"] == "SELL", "fuera de lateral, manda el PPO"
