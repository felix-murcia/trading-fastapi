from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    internal_token: str
    hmac_secret: str
    mt5_http_url: str = "http://localhost:8000"
    mt5_http_timeout: float = 10.0
    min_volume: float = 0.01
    max_volume: float = 0.50
    order_max_age_hours: int = 48
    simple_pipeline_enabled: bool = False
    paper_mode: bool = False              # Si true, simula órdenes sin ejecutarlas
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

    # ─── Market Microstructure ────────────────────────────────────────────────
    microstructure_features_enabled: bool = False  # Añade VP + Orderbook al predict (requiere retrain del modelo si se activa)
    volume_profile_bins: int = 50         # Niveles de precio para Volume Profile
    volume_profile_lookback: int = 20     # Velas para calcular VP
    orderbook_levels: int = 5             # Niveles de profundidad para Orderbook Imbalance

    class Config:
        env_file = "/app/.env"


settings = Settings()
