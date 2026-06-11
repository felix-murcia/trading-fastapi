from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    internal_token: str
    hmac_secret: str
    mt5_http_url: str = "http://192.168.0.107:8000"
    mt5_http_timeout: float = 10.0
    min_volume: float = 0.01
    max_volume: float = 0.50
    order_max_age_hours: int = 48
    simple_pipeline_enabled: bool = False
    signal_cooldown_minutes: int = 60     # H1: 1 vela = 60 min
    sl_risk_usd: float = 15.0
    sl_pct: float = 0.005                 # SL = 0.5% del precio de entrada
    rr_min: float = 2.0

    class Config:
        env_file = "/app/.env"


settings = Settings()
