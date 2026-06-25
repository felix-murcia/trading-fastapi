from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    internal_token: str
    hmac_secret: str
    mt5_http_url: str = "http://100.81.112.95:8000"
    mt5_http_timeout: float = 10.0
    min_volume: float = 0.01
    max_volume: float = 0.50
    order_max_age_hours: int = 48
    simple_pipeline_enabled: bool = False
    signal_cooldown_minutes: int = 60     # H1: 1 vela = 60 min
    sl_risk_usd: float = 15.0
    sl_pct: float = 0.001                 # SL = 0.1% del precio de entrada
    rr_min: float = 1.0
    news_filter_enabled: bool = True
    news_blackout_minutes: int = 15       # ±N min alrededor de noticias High impact

    class Config:
        env_file = "/app/.env"


settings = Settings()
