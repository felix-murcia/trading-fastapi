from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    internal_token: str
    hmac_secret: str
    mt5_http_url: str = "http://localhost:8000"
    mt5_http_timeout: float = 10.0
    min_volume: float = 0.01
    max_volume: float = 0.50
    max_total_positions: int = 5   # P2-4: exposición agregada de TODA la cuenta (todas las estrategias y pares)
    ppo_risk_pct: float = 0.03        # P2-2: tope de riesgo por trade del PPO (fracción del equity)
    order_max_age_hours: int = 48
    simple_pipeline_enabled: bool = True
    signal_cooldown_minutes: int = 60     # H1: 1 vela = 60 min
    sl_risk_usd: float = 15.0
    sl_risk_usd_xauusd: float = 15.0
    sl_risk_usd_xaueur: float = 15.0
    sl_mult: float = 1.5
    sl_pct: float = 0.001
    rr_min: float = 1.0
    rr_min_xau: float = 1.2
    sl_min_spread_mult: int = 3
    sl_min_pips_default: float = 3.0
    sl_min_pips_usdjpy: float = 5.0
    sl_min_pips_xaueur: float = 10.0
    sl_min_pips_xauusd: float = 10.0
    news_filter_enabled: bool = True
    news_blackout_minutes: int = 15

    # ─── Auto-Retrain ───────────────────────────────────────────────────────────
    retrain_enabled: bool = True          # Habilitar reentrenamiento automático
    trades_before_retrain: int = 50       # N trades antes de disparar retrain
    min_trades_for_retrain: int = 20     # Mínimo de trades para considerar datos suficientes
    retrain_auto_approve: bool = False    # P0-1: promoción automática nunca implícita; aprobar vía POST /api/v1/ai/retrain/approve
    retrain_interval_hours: int = 168     # P3-2d: retrain programado (7 días desde el último intento)

    # ─── P3-2b: Backup del bundle ─────────────────────────────────────────────
    model_backup_enabled: bool = True         # GCS además del backup local verificado
    model_backup_interval_hours: int = 24     # Cadencia del backup programado

    # ─── P3-2c: Monitor de drift ─────────────────────────────────────────────
    drift_check_interval_hours: int = 24      # Cadencia del check PSI + decay

    # ─── P3-1: Pares habilitados (mundos independientes, sin acoplamiento) ─────
    # Cada par construye su propio PairContext (modelo, riesgo, estado) vía
    # core.container — añadir un par NO toca el código de otro. 'enabled' es
    # obligatorio en cada especificación: listar un par nunca lo activa solo.
    # P3-3: GBPUSD canario listado pero NO habilitado — se entrena con el
    # gate canario y solo se activa (enabled=true) con el gate en verde
    # (límites del canario: daily_loss 2%, equity 20%).
    # Env (JSON): PAIRS='[{"symbol":"EURUSD","enabled":true},{"symbol":"GBPUSD","enabled":true}]'
    pairs: list[dict] = [
        {"symbol": "EURUSD", "enabled": True},
        {"symbol": "GBPUSD", "enabled": False,
         "daily_loss_pct": 2.0, "equity_allocation_pct": 20},
    ]

    # ─── P3-3: bloqueo por drawdown de cuenta (canario = 15%) ───────────────────
    # None (default) = solo alerta al 10% DD (comportamiento histórico).
    # Valor (p.ej. 15.0) = el guardian BLOQUEA aperturas nuevas cuando el
    # equity cae ≥ ese % desde el peak. Es de cuenta: aplica a todos los pares.
    max_drawdown_block_pct: float | None = None

    # ─── P0 / H1: gate de confianza de entrada (Félix 2026-10-09) ─────────────
    # Umbral mínimo de `effective_prob` (ml_prob × confidence_modifier) para
    # adelantar una propuesta BUY/SELL. 0.0 (default) = MECANISMO DESARMADO
    # (no-op: toda propuesta pasa, cero cambio de comportamiento).
    # Evidencia EN CONTRA pre-registrada en README (muestra n=18 de trades
    # 229–250): las bandas de confianza son INVERSIAS (<0.5 = +4.58 €/trade;
    # ≥0.7 = −1.89 €/trade) — un gate armado habría restado dinero esa
    # semana. Armar SOLO con (a) revisión N≥50 trades post-fix Qwen que lo
    # justifique, o (b) orden explícita de Félix (env MIN_ENTRY_CONFIDENCE).
    min_entry_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # ─── Parche del agujero del feed (Félix 2026-10-12) ──────────────────────
    # El feed del MCP corta la historia en 2025-12-31 y sólo entrega el día
    # vivo (agujero 2026-01-01→10-11). Los CSV de `resultadosBT/` (MT5, UTC
    # puro) son la única fuente de 2026. Directorio con esos CSV; vacío =
    # parche desactivado (el feed pasa tal cual).
    csv_history_dir: str = ""

    # ─── Market Microstructure ────────────────────────────────────────────────
    microstructure_features_enabled: bool = False  # Añade VP + Orderbook al predict (requiere retrain del modelo si se activa)
    volume_profile_bins: int = 50         # Niveles de precio para Volume Profile
    volume_profile_lookback: int = 20     # Velas para calcular VP
    orderbook_levels: int = 5             # Niveles de profundidad para Orderbook Imbalance

    class Config:
        env_file = "/app/.env"


settings = Settings()
